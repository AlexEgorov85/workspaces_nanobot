"""ApplicationContext — единая точка создания и связывания сервисов.

Создаёт все общие сервисы (конфиг, БД-логирование, аудит-сервисы,
шина сообщений, хранилище сессий, агент) и публикует их атрибутами.
Точки входа (gateway.py / cli_agent.py) — тонкие оркестраторы,
использующие ``ctx`` для запуска/остановки.

Все тяжёлые зависимости (nanobot, psycopg2) импортируются лениво —
модуль безопасно импортировать даже в тестовых средах.

Composition contract
====================

``ApplicationContext.create()`` принимает ТОЛЬКО typed-параметры:

  * обязательный ``role: Literal["gateway", "cli"]``;
  * явные override-ключи ``storage_override``, ``session_override``;
  * ``**kwargs`` — временная compatibility boundary для deprecated
    ``enable_db_logging / enable_audit / enable_cron / print_llm_calls /
    profile`` (см. AGENTS.md § «Working Conventions → Configuration»).

``role`` определяет только composition инфраструктуры
(``PostgresChannel``, ``CronService``); НЕ определяет cache owner/reader —
это ответственность ``CacheOwnershipCoordinator`` (см.
``lib/services/cache_ownership.py``).

Cache owner/reader status — НЕ через ``role``:

  * role="gateway" может быть OWNER (если пришёл первый к PG claim) или
    READER (если первым пришёл CLI);
  * role="cli" — то же самое;
  * Оба процесса открывают ``cache.duckdb`` через concrete factory
    ``DuckDbCacheStore.open(path, mode)`` с mode от ``coord.try_claim()``.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Deprecated kwargs, принимаются ТОЛЬКО через **kwargs до раскрытия
# change ``remove-deprecated-enable-kwargs``. Production code MUST NOT
# их использовать. См. openspec/changes/unify-cli-gateway-architecture
# design D1 «Staged implementation» и Stage G.
DEPRECATED_ENABLE_KWARGS = frozenset({
    "enable_db_logging",
    "enable_audit",
    "enable_cron",
    "print_llm_calls",
})


def _resolve_enable_kwargs(
    kwargs: dict[str, Any],
    *,
    gateway_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Извлечь deprecated ``enable_*``/``print_llm_calls`` из ``**kwargs``.

    Возвращает dict со всеми DEPRECATED_ENABLE_KWARGS (defaults из
    ``gateway.*`` settings). При передаче kwarg — поднимает
    ``DeprecationWarning`` (через ``warnings.warn`` с ``stacklevel=2``,
    чтобы указывать на caller'а, а не на эту функцию).

    Production code MUST NOT передавать эти kwargs напрямую —
    использовать вместо этого ``gateway.enable_*`` в SETTINGS.
    """
    import warnings

    gateways = gateway_settings or {}
    defaults = {
        "enable_db_logging": bool(gateways.get("enable_db_logging", True)),
        "enable_audit": bool(gateways.get("enable_audit", True)),
        "enable_cron": bool(gateways.get("enable_cron", False)),
        "print_llm_calls": bool(gateways.get("print_llm_calls", False)),
    }

    out = dict(defaults)
    for key, value in kwargs.items():
        if key in DEPRECATED_ENABLE_KWARGS:
            warnings.warn(
                f"ApplicationContext.create({key}={value!r}) is deprecated; "
                f"configure gateway.{key} in project.json instead. "
                "This compatibility boundary will be removed by change "
                "remove-deprecated-enable-kwargs.",
                DeprecationWarning,
                stacklevel=3,
            )
            out[key] = bool(value)
    return out


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
    cache_provider: Any | None = None  # CacheProvider ABC instance (Stage D)
    cache_store: Any | None = None  # legacy alias for cache_provider
    ownership_coordinator: Any | None = None  # CacheOwnershipCoordinator (Stage C)

    # Composition role (Stage A)
    role: str = ""  # "gateway" | "cli"
    enable_db_logging: bool = True
    enable_audit: bool = True
    enable_cron: bool = False
    print_llm_calls: bool = False

    # Storage-hybridization: cold-storage mirror для сессий + LLM usage.
    session_cold_sync_service: Any | None = None
    usage_store: Any | None = None

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
    runtime_events_subscriber: Any | None = None  # RuntimeEventsSubscriber

    @classmethod
    def create(
        cls,
        script_dir: Path,
        workspace_dir: Path,
        *,
        role: Literal["gateway", "cli"],
        storage_override: str | None = None,
        session_override: str | None = None,
        **kwargs: Any,
    ) -> ApplicationContext:
        """Собрать контекст приложения.

        Args:
            script_dir: корень проекта (где лежит config.json).
            workspace_dir: корень workspace.
            role: точка входа (``"gateway"`` или ``"cli"``). Определяет
                composition инфраструктуры (``PostgresChannel`` только в
                gateway, ``CronService`` только в gateway). НЕ определяет
                cache owner/reader — это ответственность
                ``CacheOwnershipCoordinator``.
            storage_override: режим хранилища из CLI (auto/postgres/file).
            session_override: имя сессии (CLI).
            **kwargs: deprecated compatibility boundary для

                * ``profile`` (str | None);
                * ``enable_db_logging`` (bool);
                * ``enable_audit`` (bool);
                * ``enable_cron`` (bool);
                * ``print_llm_calls`` (bool).

                Принимаются с ``DeprecationWarning`` + применяются как
                override над ``SETTINGS["gateway"].*``. После раскрытия
                change ``remove-deprecated-enable-kwargs`` — ``TypeError``.

        Raises:
            ConfigurationError: если ``_initialize_settings(profile)`` ещё не
                выполнен (proxy остался uninitialized).
        """
        # Делегируем ``**kwargs`` валидацию/применение (с DeprecationWarning).
        import config as _config
        ctx_settings = _config.SETTINGS
        # Touching ``["profile"]`` материализует ConfigurationError на
        # uninitialized proxy, но не делает duplicated work в happy-path.
        resolved_profile = ctx_settings["profile"]
        if "profile" in kwargs:
            profile = kwargs.pop("profile")
            if profile is not None and profile != resolved_profile:
                # entrypoint передал ``profile``, отличный от уже
                # инициализированного. Это явное нарушение lifecycle —
                # fail-fast через ConfigurationError boundary.
                from config import ConfigurationError
                raise ConfigurationError(
                    f"ApplicationContext.create(profile={profile!r}) called "
                    f"but SETTINGS already initialized for profile={resolved_profile!r}. "
                    "Application entrypoint must pass the same --profile value as "
                    "was passed to config._initialize_settings()."
                )

        gateways = ctx_settings.get("gateway") or {}
        enable_kwargs = _resolve_enable_kwargs(
            kwargs, gateway_settings=gateways
        )

        ctx = cls()
        ctx.script_dir = Path(script_dir)
        ctx.workspace_dir = Path(workspace_dir)
        ctx.profile = resolved_profile
        ctx.role = role
        ctx.enable_db_logging = bool(enable_kwargs["enable_db_logging"])
        ctx.enable_audit = bool(enable_kwargs["enable_audit"])
        ctx.enable_cron = bool(enable_kwargs["enable_cron"])
        ctx.print_llm_calls = bool(enable_kwargs["print_llm_calls"])

        # Сбросить ``TableRegistry`` — это singleton, и при повторном
        # ``create()`` в одном процессе (тесты, streamlit-reload, gateway
        # перезапуск конфига) старые регистрации остались бы и смешались
        # с новыми. ``_make_sync_services`` и ``_auto_register_skills``
        # ниже заполнят реестр заново.
        from lib.services.table_registry import table_registry
        table_registry.clear()

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
                return_file_manager=not ctx.enable_cron,
            )
        except Exception as exc:
            logger.warning("SessionStorageService failed: %s", exc)
            storage_mode, session_manager = "file", None

        ctx.storage_mode = storage_mode
        ctx.session_manager = session_manager

        # 4. DbLoggingService
        if ctx.enable_db_logging:
            ctx.db_logging_service = _make_db_logging(ctx)

        # 4a. LLMUsageStore (upstream observer storage).
        # См. спеку ``storage/usage-store``. Всегда создаётся —
        # фабрика вернёт ``None`` если конфиг отключён / nanobot
        # не предоставляет класс.
        ctx.usage_store = _make_usage_store(ctx)

        # 4b. SessionColdSyncService (cold-storage mirror).
        # Создаётся только при PG-конфиге. Sync стартует позже,
        # в ``start()`` lifecycle.
        ctx.session_cold_sync_service = _make_session_cold_sync_service(ctx)

        # 5. CacheOwnershipCoordinator + cache_provider + sync service
        if ctx.enable_audit:
            _auto_register_skills(ctx)
            _register_infra_resources(ctx)
            (
                ctx.cache_provider,
                ctx.sync_service,
                _ownership_coord,
            ) = _make_sync_services(ctx)
            ctx.ownership_coordinator = _ownership_coord
            # Back-compat alias — runtime code/project tools/runtime
            # patches всё ещё ожидают ``ctx.cache_store`` (rename в
            # Stage D). После migrate callers на новый interface alias
            # может быть удалён.
            ctx.cache_store = ctx.cache_provider

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

        # CronService — ТОЛЬКО для role="gateway". При role="cli" значение
        # ``gateway.enable_cron`` MUST быть проигнорировано (см.
        # openspec/changes/unify-cli-gateway-architecture design D7 —
        # «Cron = gateway-only»). Решает проблему «два процесса выполняют
        # один jobs.json дважды».
        cron_service = None
        if ctx.enable_cron and ctx.role == "gateway":
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
            print_llm_calls=ctx.print_llm_calls,
            usage_store=ctx.usage_store,
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
        _emit_patch_inventory_banner(patch_report)

        # 7a. Project tools registration — независимый stage composition
        # root'а (см. openspec/changes/runtime-patcher-composition-cleanup,
        # design Decision 3). ``register_project_tools`` НЕ вызывается из
        # ``RuntimePatcher.apply_all()`` — это отдельный вызов, source
        # of truth для ``_emit_project_tools_inventory_banner``.
        from lib.services.project_tool_loader import register_project_tools

        project_tools_result = register_project_tools(
            agent=ctx.agent,
            workspace_dir=ctx.workspace_dir,
            settings=ctx.settings,
            cache_store=ctx.cache_store,
            db_logging_service=ctx.db_logging_service,
        )
        ctx.project_tools_result = project_tools_result
        _emit_project_tools_inventory_banner(project_tools_result)

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

        # Pre-startup проверка наличия обязательных runtime-таблиц
        # (6 имён из SETTINGS["channels"]["postgres"] +
        # SETTINGS["logging"]["db"]). При отсутствии любой — выброс
        # SchemaValidationError (наследник ConfigurationError), который
        # ловится в gateway.main() / cli_agent.main() → exit 2 + stderr.
        # Без этой проверки gateway стартует, а сервисы падают уже
        # на первой INSERT/SELECT в несуществующие таблицы.
        # См. openspec/specs/runtime/startup-schema-validation.
        self._validate_runtime_schema()

        # Подписчик на runtime-события nanobot 0.3.5.
        # Регистрируется ПОСЛЕ apply_all (если он активен) и ДО старта каналов,
        # чтобы seed лимита окна/модели + метрики оборота были доступны
        # для первого inbound-сообщения. Lifecycle:
        # start() здесь → каналы стартуют → stop() в _stop_runtime_events_subscriber
        # ДО MessageBus.drain() в shutdown-последовательности.
        # См. openspec/changes/runtime-events-subscription.
        try:
            from lib.services.runtime_events_subscriber import (
                RuntimeEventsSubscriber,
            )
            self.runtime_events_subscriber = RuntimeEventsSubscriber(
                self.bus,
                db_logging_service=self.db_logging_service,
            )
            self.runtime_events_subscriber.start()
            if self._shutdown is not None:
                self._shutdown.register(
                    "runtime_events_subscriber",
                    self.runtime_events_subscriber,
                )
        except Exception as exc:
            logger.warning(
                "RuntimeEventsSubscriber not started: %s", exc
            )

        if self.db_logging_service is not None:
            self.db_logging_service.start()
            self._shutdown.register("db_logging_service", self.db_logging_service)

        if self.sync_service is not None:
            try:
                self.sync_service.start(initial_load=True)
                self._shutdown.register("sync_service", self.sync_service)
            except Exception as exc:
                logger.warning("PgDuckDbSyncService not started: %s", exc)

        if self.session_cold_sync_service is not None:
            try:
                self.session_cold_sync_service.start()
                self._shutdown.register(
                    "session_cold_sync_service",
                    self.session_cold_sync_service,
                )
            except Exception as exc:
                logger.warning(
                    "SessionColdSyncService not started: %s", exc
                )

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
        # MessageBus.drain() ожидает завершения in-flight handler'ов
        # (например, _handle_turn_completed ещё может писать в БД через
        # DbLoggingService с батчевым flush). Вызываем ПОСЛЕ остановки
        # сервисов (channels/sync) и ДО остановки RuntimeEventsSubscriber.
        # Если bus не имеет drain() (защита от nanobot < 0.3.5) — no-op.
        # См. openspec/changes/runtime-events-subscription/design.md D7.
        bus = getattr(self, "bus", None)
        if bus is not None and hasattr(bus, "drain"):
            try:
                drain = bus.drain
                if inspect.iscoroutinefunction(drain):
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(drain())
                        else:
                            loop.run_until_complete(drain())
                    except RuntimeError:
                        pass
                else:
                    drain()
            except Exception as exc:
                logger.warning("MessageBus.drain failed: %s", exc)
        # RuntimeEventsSubscriber.stop() — после drain, чтобы in-flight
        # handler'ы гарантированно отработали.
        if getattr(self, "runtime_events_subscriber", None) is not None:
            try:
                self.runtime_events_subscriber.stop()
            except Exception as exc:
                logger.warning(
                    "RuntimeEventsSubscriber.stop failed: %s", exc
                )
        # Close LLM usage store (SQLite WAL).
        if self.usage_store is not None:
            try:
                self.usage_store.close()
            except Exception as exc:
                logger.warning("usage_store.close failed: %s", exc)
        # Ownership release — Stage E. При shutdown coordinator.release()
        # удаляет строку claim из ``agent_cache_ownership`` для
        # следующего takeover'а (или kill -9 потом expire'нется).
        if self.ownership_coordinator is not None:
            try:
                self.ownership_coordinator.release()
            except Exception as exc:
                logger.warning("ownership_coordinator.release failed: %s", exc)
        # После остановки сервисов закрываем общий пул соединений.
        _stop_db_pool()
        if self.runtime_health is not None:
            self.runtime_health.mark_stopped()
        self._started = False

    def _validate_runtime_schema(self) -> None:
        """Pre-startup проверка наличия обязательных runtime-таблиц.

        Вызывается из ``start()`` сразу после ``_start_db_pool()`` и
        до подъёма каналов/``db_logging_service``/``sync_service``.
        Имена таблиц берутся из ``self.settings`` (6 ключей:
        ``channels.postgres.{table_name,messages_table,meta_table,
        claims_table}`` + ``logging.db.{table_name,question_runs_table}``)
        — никаких литералов в коде.

        При отсутствии любой таблицы — ``SchemaValidationError``
        (наследник ``ConfigurationError``). Покрывается
        ``gateway.main()`` / ``cli_agent.main()`` startup-boundary →
        ``exit 2`` + ``stderr``.

        Опциональный gate ``gateway.startup.schema_validation.enabled``
        (``True`` по умолчанию) позволяет временно пропустить
        проверку (например, при аварийном деплое).

        См. ``openspec/specs/runtime/startup-schema-validation``.
        """
        try:
            settings = self.settings or {}
        except Exception:
            settings = {}
        gateway_cfg = settings.get("gateway") or {}
        startup_cfg = gateway_cfg.get("startup") or {}
        schema_cfg = startup_cfg.get("schema_validation") or {}
        enabled = schema_cfg.get("enabled", True)
        timeout_sec = float(schema_cfg.get("timeout_sec", 5.0))
        if not enabled:
            logger.warning(
                "startup schema validation is disabled "
                "(gateway.startup.schema_validation.enabled=false)"
            )
            return
        try:
            from utils.db import fetch as _db_fetch
            from lib.services.schema_validation import SchemaValidationService
        except Exception as exc:
            # Если зависимости не загрузились — это серьёзная проблема,
            # но не блокируем startup (раньше без этой проверки gateway
            # всё равно бы упал позже). Логируем warning и пропускаем.
            logger.warning("startup schema validation skipped: %s", exc)
            return
        # ``SchemaValidationService.validate`` бросает ``SchemaValidationError``
        # (наследник ``ConfigurationError``) при missing — пусть поднимется
        # до ``gateway.main()`` / ``cli_agent.main()``.
        SchemaValidationService.validate(
            settings,
            fetch=_db_fetch,
            timeout_sec=timeout_sec,
        )


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
    _emit_hook_inventory_banner(ctx)


def _emit_hook_inventory_banner(ctx: ApplicationContext) -> None:
    """Промпт-сводка по хукам через ``runtime_inventory.diff_hooks``.

    Печатает:
      * (нет вывода) — все required хуки на месте, нет unexpected;
      * жёлтый блок — missing_optional / unexpected (не критично);
      * красный блок — missing_required / missing_factory (нужно внимание).

    Срабатывает ПОСЛЕ обычного ``Hooks connected: ...`` лога; использует
    ``rich.console.Console`` с явными цветами/рамочкой, чтобы в глаза
    бросалось даже если loguru/WARNING уровень подавлен.
    """
    from lib.services.runtime_inventory import collect_actual_hook_names, diff_hooks

    actual_names, factory_count = collect_actual_hook_names(ctx)
    diff = diff_hooks(actual_names, actual_factory_count=factory_count)

    has_issue = (
        diff["missing_required"] or diff["missing_factory"] or diff["unexpected"]
    )
    has_warn = diff["missing_optional"]
    if not has_issue and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_issue:
            style = "bold red"
            header = "HOOK INVENTORY: critical drift detected"
        else:
            style = "bold yellow"
            header = "HOOK INVENTORY: optional drift"

        lines: list[str] = []
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["missing_factory"]:
            lines.append(
                f"[red]MISSING FACTORY:[/red] {', '.join(diff['missing_factory'])}"
            )
        if diff["unexpected"]:
            lines.append(
                f"[yellow]UNEXPECTED:[/yellow] {', '.join(diff['unexpected'])}"
            )
        if diff["missing_optional"]:
            lines.append(
                f"[yellow]MISSING OPTIONAL:[/yellow] {', '.join(diff['missing_optional'])}"
            )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        # Fallback на plain stderr, чтобы не потерять диагностику
        # в средах без rich (например, при cp1251 + minimal Python).
        import sys
        sys.stderr.write(
            f"HOOK INVENTORY: missing_required={diff['missing_required']} "
            f"unexpected={diff['unexpected']} "
            f"missing_factory={diff['missing_factory']} "
            f"missing_optional={diff['missing_optional']} ({exc})\n"
        )


def _emit_patch_inventory_banner(patch_report: Any) -> None:
    """Промпт-сводка по runtime-патчам через ``runtime_inventory.diff_runtime_patches``.

    Печатает красный блок, если required-патч fail'ит или не запустился;
    жёлтый — если есть failed optional (полезно видеть, но не ломает
    runtime). Срабатывает ПОСЛЕ обычного ``Runtime patches:`` лога.
    """
    from lib.services.runtime_inventory import diff_runtime_patches

    diff = diff_runtime_patches(
        applied=list(patch_report.applied),
        skipped=list(patch_report.skipped),
        failed=list(patch_report.failed),
    )

    has_critical = diff["missing_required"] or diff["failed_required"]
    has_warn = bool(patch_report.failed) and not has_critical
    if not has_critical and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_critical:
            style = "bold red"
            header = "RUNTIME PATCH INVENTORY: critical drift"
        else:
            style = "bold yellow"
            header = "RUNTIME PATCH INVENTORY: optional patches failed"

        lines: list[str] = []
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["failed_required"]:
            lines.append(
                f"[red]FAILED REQUIRED:[/red] {', '.join(diff['failed_required'])}"
            )
        if has_warn:
            failed_optional = [
                n for n, _ in patch_report.failed
                if n not in diff["failed_required"]
            ]
            if failed_optional:
                lines.append(
                    f"[yellow]FAILED OPTIONAL:[/yellow] {', '.join(failed_optional)}"
                )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        import sys
        sys.stderr.write(
            f"RUNTIME PATCH INVENTORY: missing_required={diff['missing_required']} "
            f"failed_required={diff['failed_required']} "
            f"failed={list(patch_report.failed)} ({exc})\n"
        )


def _emit_project_tools_inventory_banner(project_tools_result: Any) -> None:
    """Промпт-сводка по project tools через ``runtime_inventory``.

    Использует **структурные поля** ``ProjectToolsLoadResult``
    (``registered`` / ``disabled`` / ``duplicate`` / ``failed`` /
    ``error``) **напрямую**, а не regex-парсинг ``detail``. Это:

      * даёт корректный баннер при outer-loader failure (раньше
        ``detail = "register_project_tools failed: RuntimeError: ..."``
        парсился как ``failed=["RuntimeError: ..."]`` — мусор);
      * отделяет ``loader-level error`` от ``per-tool failed``;
      * сохраняет совместимость с ``diagnose_startup.py``, который
        читает ``Custom (project) tools:`` из логов (там всё ещё
        ``detail`` — ``runtime_inventory.parse_project_tools_detail``).

    Печатает красный блок, если required tool не зарегистрировался
    (missing или failed) или loader вернул ``error``; жёлтый — если
    unexpected tool или failed optional.
    """
    if project_tools_result is None:
        return
    registered = list(getattr(project_tools_result, "registered", []) or [])
    disabled = list(getattr(project_tools_result, "disabled", []) or [])
    duplicate = list(getattr(project_tools_result, "duplicate", []) or [])
    failed = list(getattr(project_tools_result, "failed", []) or [])
    error = getattr(project_tools_result, "error", None)

    # Раньше banner анализировал detail через regex, что для outer
    # failure давало семантически неправильный результат
    # (failed = ["RuntimeError: ...]"). Сейчас diff вычисляется из
    # structured-полей напрямую.
    from lib.services.runtime_inventory import diff_project_tools

    diff = diff_project_tools(
        registered=registered,
        skipped_disabled=disabled,
        failed=failed,
    )

    has_critical = bool(diff["missing_required"] or diff["failed"] or error)
    has_warn = bool(diff["unexpected"] or diff["disabled_required"])
    if not has_critical and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_critical:
            style = "bold red"
            header = "PROJECT TOOLS INVENTORY: critical drift"
        else:
            style = "bold yellow"
            header = "PROJECT TOOLS INVENTORY: drift"

        lines: list[str] = []
        if error:
            # Outer-loader failure (``_discover`` / ``ToolContext`` / etc.)
            # — отдельная категория, не путать с per-tool failed.
            lines.append(f"[red]LOADER ERROR:[/red] {error}")
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["failed"]:
            lines.append(
                f"[red]FAILED:[/red] {', '.join(diff['failed'])}"
            )
        if diff["disabled_required"]:
            lines.append(
                f"[red]DISABLED REQUIRED:[/red] {', '.join(diff['disabled_required'])}"
            )
        if diff["unexpected"]:
            lines.append(
                f"[yellow]UNEXPECTED:[/yellow] {', '.join(diff['unexpected'])}"
            )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        import sys
        sys.stderr.write(
            f"PROJECT TOOLS INVENTORY: error={error} "
            f"missing_required={diff['missing_required']} "
            f"failed={diff['failed']} unexpected={diff['unexpected']} ({exc})\n"
        )


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
        from lib.services.db_logging_service import LogEvent, try_log_event
        try_log_event(
            ctx.db_logging_service,
            LogEvent(
                event_type="sync_skipped_registry_empty",
                level="WARN",
                session_id="gateway:sync",
                channel=None,
                actor="sync",
                name="sync_skipped_registry_empty",
                summary="PgDuckDbSyncService skipped: TableRegistry пуст",
                payload={
                    "reason": "TableRegistry пуст",
                    "detail": "Нет ни одной зарегистрированной таблицы — проверьте project.json::skills.* и gateway.vector.index.*",
                },
            ),
            producer="ApplicationContext",
            event_type="sync_skipped_registry_empty",
        )
        return None, None, None
    if not dsn:
        logger.warning(
            "PgDuckDbSyncService skipped: channels.postgres.dsn не задан "
            "(пустая строка или отсутствует ключ в project.json)."
        )
        from lib.services.db_logging_service import LogEvent, try_log_event
        try_log_event(
            ctx.db_logging_service,
            LogEvent(
                event_type="sync_skipped_no_dsn",
                level="WARN",
                session_id="gateway:sync",
                channel=None,
                actor="sync",
                name="sync_skipped_no_dsn",
                summary="PgDuckDbSyncService skipped: channels.postgres.dsn не задан",
                payload={
                    "reason": "channels.postgres.dsn не задан",
                    "detail": "DATABASE_URL пустой или отсутствует ключ в project.json — sync не сможет подключиться к PG",
                },
            ),
            producer="ApplicationContext",
            event_type="sync_skipped_no_dsn",
        )
        return None, None, None

    from lib.services.cache_ownership import (
        CacheAccessMode,
        CacheOwnershipCoordinator,
    )
    from lib.services.duckdb_cache_store import DuckDbCacheStore
    from lib.services.pg_duckdb_sync_service import PgDuckDbSyncService

    all_table_names = list(table_registry.table_names())
    vector_names = list(table_registry.vector_names())

    if not all_table_names and not vector_names:
        logger.warning(
            "PgDuckDbSyncService skipped: в TableRegistry есть ресурсы, но ни одного "
            "имени в table_names()/vector_names() — несоответствие регистрации."
        )
        from lib.services.db_logging_service import LogEvent, try_log_event
        try_log_event(
            ctx.db_logging_service,
            LogEvent(
                event_type="sync_skipped_no_table_names",
                level="WARN",
                session_id="gateway:sync",
                channel=None,
                actor="sync",
                name="sync_skipped_no_table_names",
                summary="PgDuckDbSyncService skipped: в TableRegistry есть ресурсы, но table_names()/vector_names() пусты",
                payload={
                    "reason": "в TableRegistry есть ресурсы, но table_names()/vector_names() пусты",
                    "detail": "Несоответствие регистрации — проверьте register() vs register_infra()",
                },
            ),
            producer="ApplicationContext",
            event_type="sync_skipped_no_table_names",
        )
        return None, None, None

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

    # ==== Stage C/D/B/E integration ====
    # 1. CacheOwnershipCoordinator — координатор ownership для логического
    #    cache resource ``local_cache`` через таблицу ``agent_cache_ownership``
    #    (см. sql/migrations/V005__create_agent_cache_ownership.sql).
    # 2. ``coord.try_claim()`` — atomic PG INSERT ... ON CONFLICT. Один процесс
    #    получает acquired=True (OWNER), остальные — False (READER).
    # 3. ``DuckDbCacheStore.open(path, mode)`` — concrete factory. mode
    #    зависит от результата claim:
    #      - acquired=True → READ_WRITE (OWNER может писать в cache);
    #      - acquired=False → READ_ONLY (READER, через физический read-only
    #        DuckDB connection + assertion guard в query_sql).
    worker_id = f"{ctx.role}_{os.getpid()}"
    coord = CacheOwnershipCoordinator(
        worker_id=worker_id,
        dsn=dsn,
        resource_key="local_cache",
    )
    claim = coord.try_claim()
    logger.info(
        "cache_ownership: role=%s worker_id=%s acquired=%s generation=%d "
        "current_owner=%s",
        ctx.role, worker_id, claim.acquired, claim.generation,
        claim.current_owner_id or "(none)",
    )

    mode = CacheAccessMode.READ_WRITE if claim.acquired else CacheAccessMode.READ_ONLY

    # Concrete factory — DuckDB connection opened с учётом ``mode``.
    # Если path не на локальной FS — ``UnsupportedFilesystemError`` поднимается.
    store = DuckDbCacheStore.open(
        path=publish_path,
        mode=mode,
    )
    # Конфигурируем store через конструктор args через post-init хак:
    # factory ``open()`` принимает только path/mode. Другие поля
    # (schema, tables, vector_db_table, embedding_*) настраиваются
    # отдельным вызовом или через прямой dict.
    store._publish_path = publish_path
    store._schema = schemas[0] if schemas else "main"
    store._tables = all_table_names or None
    store._vector_db_table = vector_names[0] if vector_names else ""
    store._embedding_base_url = embedding_base_url
    store._embedding_model = embedding_model
    store._embedding_dimension = embedding_dimension
    store._db_logging_service = ctx.db_logging_service

    # ``sync_service`` создаётся ТОЛЬКО если этот процесс — OWNER
    # (claim.acquired=True). READER процессы НЕ sync'ят — только читают
    # snapshot, который публикует OWNER.
    if not claim.acquired:
        logger.info(
            "cache_ownership: role=%s worker_id=%s is READER; "
            "sync_service NOT created (other process is OWNER gen=%d)",
            ctx.role, worker_id, claim.generation,
        )
        return store, None, coord

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
        ownership_coordinator=coord,
        cache_provider=store,
    )
    return store, sync, coord


def _record_sync_skipped(
    db_logging_service: Any,
    event_type: str,
    reason: str,
    detail: str,
) -> None:
    """DEPRECATED: инлайнен в ``_make_sync_services``.

    Оставлен как back-compat shim для возможных внешних callers'ов
    (на данный момент ни одного нет). Использует
    ``DbLoggingService.try_log_event`` — единый writer.
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


def _make_session_cold_sync_service(ctx: ApplicationContext) -> Any | None:
    """Создать ``SessionColdSyncService`` (cold-storage mirror).

    Сервис создаётся только если:

    - есть ``session_manager`` (upstream JSONL);
    - в PG-конфиге указан DSN (cold-storage нужен только при
      PG-деплое).

    Если условия не выполнены — возвращает ``None``.

    См. спеку ``openspec/specs/storage/session-hybridization/spec.md``
    requirement «Cold-storage mirror в PostgreSQL».
    """
    if ctx.session_manager is None:
        return None
    try:
        from config import get_setting

        pg_dsn = get_setting("channels", "postgres", "dsn", default="")
    except Exception:
        pg_dsn = ""
    if not pg_dsn:
        return None

    try:
        sync_cfg = ctx.config_service.settings_section("gateway").get(
            "session_cold_sync", {}
        )
    except Exception:
        sync_cfg = {}
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}

    enabled = bool(sync_cfg.get("enabled", True))
    sync_interval_sec = float(sync_cfg.get("sync_interval_sec", 30.0))
    batch_size = int(sync_cfg.get("batch_size", 50))
    stale_tolerance_seconds = int(sync_cfg.get("stale_tolerance_seconds", 120))
    sync_lag_threshold_seconds = int(
        sync_cfg.get("sync_lag_threshold_seconds", 3600)
    )

    schema = get_setting("channels", "postgres", "schema", default="public")
    meta_table = get_setting("channels", "postgres", "meta_table",
                            default="agent_session_meta")
    messages_table = get_setting("channels", "postgres", "messages_table",
                                default="agent_session_messages")

    from lib.services.session_cold_sync_service import SessionColdSyncService

    logger.info(
        'session_cold_sync: stale_tolerance=%ss, '
        'sync_lag_threshold=%ss, sync_interval=%ss, batch=%d, enabled=%s',
        stale_tolerance_seconds,
        sync_lag_threshold_seconds,
        sync_interval_sec,
        batch_size,
        enabled,
    )

    return SessionColdSyncService(
        session_manager=ctx.session_manager,
        pg_dsn=pg_dsn,
        schema=schema,
        meta_table=meta_table,
        messages_table=messages_table,
        sync_interval_sec=sync_interval_sec,
        batch_size=batch_size,
        enabled=enabled,
        db_logging_service=ctx.db_logging_service,
        stale_tolerance_seconds=stale_tolerance_seconds,
        sync_lag_threshold_seconds=sync_lag_threshold_seconds,
    )


def _make_usage_store(ctx: ApplicationContext) -> Any | None:
    """Создать ``LLMUsageStore`` (upstream nanobot) по конфигу.

    Конфиг — ``gateway.usage_store.*`` (``sqlite_path``,
    ``enabled``). Возвращает ``None`` если отключено.

    См. спеку ``openspec/specs/storage/usage-store/spec.md``.
    """
    try:
        usage_cfg = ctx.config_service.settings_section("gateway").get(
            "usage_store", None
        )
    except Exception:
        usage_cfg = None
    from lib.services.llm_usage_store_factory import create_usage_store

    return create_usage_store(usage_cfg)


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
